"""Background star and trail rendering worker."""

from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from PyQt5.QtCore import (
    QObject,
    QPointF,
    Qt,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from TerraLab.common.exception_reporting import log_suppressed_exception

def _compute_trail_segments_chunk(args):
    """
    Worker function for parallel star-trail math.

    NumPy releases the GIL for the expensive vector operations, so threads
    avoid creating child processes that could outlive the Qt canvas.
    """
    (
        ra,
        dec,
        f_r,
        f_g,
        f_b,
        step_lsts,
        sin_lat,
        cos_lat,
        scale_h,
        cx,
        cy_base,
        y_center_val,
        cam_az_rad,
        jump_threshold,
        cancel_event,
    ) = args

    import numpy as np

    # 1. Physics (Broadcasting)
    ra_col = ra[:, np.newaxis]
    ha = step_lsts[np.newaxis, :] - ra_col
    ha_rad = np.radians(ha)
    dec_rad = np.radians(dec)[:, np.newaxis]

    sin_dec = np.sin(dec_rad)
    cos_dec = np.cos(dec_rad)

    sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * np.cos(ha_rad)
    sin_alt = np.clip(sin_alt, -1.0, 1.0)
    alt_rad = np.arcsin(sin_alt)

    cos_alt = np.cos(alt_rad)
    cos_az_num = sin_dec - sin_alt * sin_lat
    cos_az_den = cos_alt * cos_lat + 1e-10
    cos_az = np.clip(cos_az_num / cos_az_den, -1.0, 1.0)
    az_rad = np.arccos(cos_az)
    sin_ha = np.sin(ha_rad)
    az_rad = np.where(sin_ha > 0, 2 * np.pi - az_rad, az_rad)

    # 2. Projection
    az_rel_rad = az_rad - cam_az_rad
    cos_alt = np.cos(alt_rad)
    sin_alt = np.sin(alt_rad)
    cos_az_rel = np.cos(az_rel_rad)
    sin_az_rel = np.sin(az_rel_rad)

    denom = 1.0 + cos_alt * cos_az_rel
    invalid = denom < 1e-6
    k = np.where(invalid, 0.0, 2.0 / denom)
    x = k * cos_alt * sin_az_rel
    y = k * sin_alt

    sx = cx + x * scale_h
    sy = cy_base - (y - y_center_val) * scale_h

    # 3. Segments
    results = []
    for i in range(len(ra)):
        if cancel_event.is_set():
            return []
        row_inv = invalid[i]
        if np.all(row_inv):
            continue

        row_x = sx[i]
        row_y = sy[i]

        valid_idxs = np.where(~row_inv)[0]
        if len(valid_idxs) < 2:
            continue

        diffs = np.diff(valid_idxs)
        breaks = np.where(diffs > 1)[0]

        starts = [valid_idxs[0]]
        ends = []
        for b in breaks:
            ends.append(valid_idxs[b])
            starts.append(valid_idxs[b + 1])
        ends.append(valid_idxs[-1])

        star_segments = []
        for s_idx, e_idx in zip(starts, ends):
            chunk_x = row_x[s_idx : e_idx + 1]
            chunk_y = row_y[s_idx : e_idx + 1]
            if len(chunk_x) < 2:
                continue

            dx = np.abs(np.diff(chunk_x))
            dy = np.abs(np.diff(chunk_y))
            jumps = (dx + dy) > jump_threshold

            if np.any(jumps):
                j_locs = np.where(jumps)[0]
                c_starts = [0]
                c_ends = []
                for jl in j_locs:
                    c_ends.append(jl)
                    c_starts.append(jl + 1)
                c_ends.append(len(chunk_x) - 1)
                for cs, ce in zip(c_starts, c_ends):
                    if ce >= cs:
                        star_segments.append(
                            (chunk_x[cs : ce + 1], chunk_y[cs : ce + 1])
                        )
            else:
                star_segments.append((chunk_x, chunk_y))

        if star_segments:
            results.append(
                ((int(f_r[i]), int(f_g[i]), int(f_b[i])), star_segments)
            )

    return results


class StarRenderWorker(QObject):
    result_ready = pyqtSignal(QImage, list)  # image, visible_stars_list
    trails_ready = pyqtSignal(QImage)  # trail image

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._shutdown_event = threading.Event()

    def request_shutdown(self) -> None:
        """Thread-safe cancellation requested by the owning canvas."""

        self._shutdown_event.set()

    @pyqtSlot(dict)
    def render(self, params):
        if self._shutdown_event.is_set():
            return
        # params: dict with all data needed
        try:
            width = params["width"]
            height = params["height"]
            zoom_level = params["zoom_level"]
            vertical_ratio = params["vertical_ratio"]
            elevation_angle = params["elevation_angle"]
            azimuth_offset = params["azimuth_offset"]

            # Arrays
            ra = params["ra"]
            dec = params["dec"]
            mag = params["mag"]
            r_arr = params["r"]
            g_arr = params["g"]
            b_arr = params["b"]

            # Physics
            lst = params["lst"]
            lat_rad = params["lat_rad"]
        except KeyError as e:
            print(f"[StarRenderWorker] Missing Param: {e}")
            return

        try:
            # ... Logic continues ...

            # Limits
            local_limit = params["mag_limit"]
            star_scale = params["star_scale"]

            # Extract Bortle and Auto Flag for worker extinction
            bortle = params.get("bortle", 1)
            is_auto = params.get("is_auto", False)

            # Create QImage
            img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            painter = QPainter(img)
            painter.setRenderHint(QPainter.Antialiasing, True)

            # --- Vectorized Logic (Copied & Adapted) ---

            # Mask 1: Mag Limit
            limit_buffer = local_limit + 1.0
            mask = mag < limit_buffer
            idxs = np.where(mask)[0]
            if len(idxs) == 0:
                painter.end()
                self.result_ready.emit(img, [])
                return

            # Subset
            f_indices = idxs
            f_ra = ra[idxs]
            f_dec = dec[idxs]
            f_mag = mag[idxs]
            f_r = r_arr[idxs]
            f_g = g_arr[idxs]
            f_b = b_arr[idxs]

            # Physics
            ha = lst - f_ra
            ha_rad = np.radians(ha)
            dec_rad = np.radians(f_dec)

            sin_lat = math.sin(lat_rad)
            cos_lat = math.cos(lat_rad)
            sin_dec = np.sin(dec_rad)
            cos_dec = np.cos(dec_rad)

            sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * np.cos(ha_rad)
            # Clip
            sin_alt = np.clip(sin_alt, -1.0, 1.0)
            alt_rad = np.arcsin(sin_alt)  # radians
            alt_deg = np.degrees(alt_rad)

            # --- ATMOSPHERIC EXTINCTION (AIRMASS) ---
            h_capped = np.maximum(0.1, alt_deg)
            airmass = 1.0 / (
                np.sin(np.radians(h_capped))
                + 0.15 * (h_capped + 3.885) ** -1.253
            )
            k_ext = float(params.get("extinction_coeff", 0.20))
            airmass_penalty = k_ext * (airmass - 1.0)
            local_limit = local_limit - airmass_penalty

            # --- ATMOSPHERIC REFRACTION ---
            refraction_deg = (1.02 / 60.0) / np.tan(
                np.radians(h_capped + 10.3 / (h_capped + 5.11))
            )
            alt_deg_refined = alt_deg + refraction_deg

            # Azimuth
            cos_alt = np.cos(alt_rad)
            cos_az_num = sin_dec - sin_alt * sin_lat
            cos_az_den = cos_alt * cos_lat + 1e-10
            cos_az = np.clip(cos_az_num / cos_az_den, -1.0, 1.0)
            az_rad = np.arccos(cos_az)
            sin_ha = np.sin(ha_rad)
            az_rad = np.where(sin_ha > 0, 2 * np.pi - az_rad, az_rad)
            az_deg = np.degrees(az_rad)

            # Use refined alt for projection
            alt_rad = np.radians(alt_deg_refined)

            # 3. Local Horizon Extinction (City domes) in Background Thread
            # This must match draw_stars_numpy logic exactly
            if is_auto and params.get("horizon_profile"):
                prof = params["horizon_profile"]
                if hasattr(prof, "light_domes") and len(prof.light_domes) > 0:
                    # Bortle-based intensity scaling (B1=0%, B5=50%, B9=100%)
                    lp_intensity_factor = max(0.0, (bortle - 1.0) / 8.0)

                    az_indices = (az_deg * 2.0).astype(int) % len(
                        prof.light_domes
                    )
                    intensity = prof.light_domes[az_indices]
                    dist = prof.light_peak_distances[az_indices]

                    # DYNAMIC RADIUS (a0) & PENALTY logic matching main thread
                    dist_factor = np.exp(-dist / 35000.0)
                    log_intensity = np.log10(1.0 + intensity)
                    a0 = np.maximum(1.0, log_intensity * 8.0 * dist_factor)

                    i_alpha = (
                        (intensity**0.40)
                        * lp_intensity_factor
                        * np.exp(-((alt_deg / a0) ** 2))
                    )

                    extinction_penalty = i_alpha * 0.15
                    local_limit = local_limit - extinction_penalty

            # Projection - UNIVERSAL STEREOGRAPHIC (Horizon Centered)
            # Match Star Render Worker to Main Thread

            w, h = width, height
            scale_h = h / 2.0 * zoom_level
            cx = w / 2.0
            cy_base = h / 2.0 + (h * vertical_ratio)

            # View Shift
            elev_cam_rad = math.radians(elevation_angle)
            y_center_val = 2.0 * math.tan(elev_cam_rad / 2.0)

            # Coords are Radians already
            # p_lat = alt_rad, p_lon = az_rad
            # We need RELATIVE AZIMUTH (Az - CamAz)
            cam_az_rad = math.radians(azimuth_offset)
            az_rel_rad = az_rad - cam_az_rad

            cos_alt = np.cos(alt_rad)
            sin_alt = np.sin(alt_rad)
            cos_az = np.cos(az_rel_rad)
            sin_az = np.sin(az_rel_rad)

            denom = 1.0 + cos_alt * cos_az

            # Mask 2: Valid Projection
            mask_proj = denom > 1e-6
            if not np.any(mask_proj):
                painter.end()
                self.result_ready.emit(img, [])
                return

            # Filter Mask 2
            f_indices = f_indices[mask_proj]
            f_mag = f_mag[mask_proj]
            f_r = f_r[mask_proj]
            f_g = f_g[mask_proj]
            f_b = f_b[mask_proj]

            # Re-slice arrays
            denom = denom[mask_proj]
            cos_alt = cos_alt[mask_proj]
            sin_alt = sin_alt[mask_proj]
            sin_az = sin_az[mask_proj]

            k = 2.0 / denom
            x = k * cos_alt * sin_az
            y = k * sin_alt

            sx = cx + x * scale_h
            sy = cy_base - (y - y_center_val) * scale_h

            # Bounds
            mask_screen = (
                (sx > -10)
                & (sx < width + 10)
                & (sy > -10)
                & (sy < height + 10)
            )
            if not np.any(mask_screen):
                painter.end()
                self.result_ready.emit(img, [])
                return

            f_indices = f_indices[mask_screen]
            sx = sx[mask_screen]
            sy = sy[mask_screen]
            f_mag = f_mag[mask_screen]
            f_r = f_r[mask_screen]
            f_g = f_g[mask_screen]
            f_b = f_b[mask_screen]

            # Drawing
            diff = local_limit - f_mag
            fade_in = np.clip(diff * 2.0, 0.0, 1.0)
            eff_alpha = np.where(f_mag < 2.0, np.sqrt(fade_in), fade_in)
            mask_vis = eff_alpha > 0.01

            f_indices = f_indices[mask_vis]
            sx = sx[mask_vis]
            sy = sy[mask_vis]
            f_mag = f_mag[mask_vis]
            eff_alpha = eff_alpha[mask_vis]
            f_r = f_r[mask_vis]
            f_g = f_g[mask_vis]
            f_b = f_b[mask_vis]

            painter.setPen(Qt.NoPen)
            visible_stars_out = []
            cel_objs = params["cel_objs_ref"]  # list of dicts {id, ra, dec...}

            # Loop
            count = len(sx)
            for i in range(count):
                if i % 1024 == 0 and self._shutdown_event.is_set():
                    painter.end()
                    return
                x, y = sx[i], sy[i]
                mag = f_mag[i]
                alpha_f = eff_alpha[i]

                # Interaction Data
                idx = f_indices[i]
                if 0 <= idx < len(cel_objs):
                    # Append small tuple: (x, y, object)
                    visible_stars_out.append((x, y, cel_objs[idx]))

                # Prevent QPointF overflow or crash with extreme zoom
                if abs(x) > 200000 or abs(y) > 200000:
                    continue

                r_val, g_val, b_val = int(f_r[i]), int(f_g[i]), int(f_b[i])

                if params.get("pure_colors", False):
                    # LEGACY BEHAVIOR: Just a colored circle (Stellarium bloom/blur completely off)
                    if mag > 5.0:
                        size = max(1.0, 1.2 * star_scale)
                        a_val = 200 - min(150, int((mag - 5.0) * 10))
                        painter.setBrush(
                            QColor(
                                r_val,
                                g_val,
                                b_val,
                                int(max(50, a_val) * alpha_f),
                            )
                        )
                        painter.drawEllipse(QPointF(x, y), size, size)
                    else:
                        size = max(1.5, (5.0 - mag) * 0.8 * star_scale)
                        painter.setBrush(
                            QColor(r_val, g_val, b_val, int(255 * alpha_f))
                        )
                        painter.drawEllipse(QPointF(x, y), size, size)
                else:
                    # REALISTIC STELLARIUM OPTICS: Bloom, Clamp, Desaturation
                    if mag > 5.0:
                        # Màxim d'1 píxel real, independentment del zoom per
                        # evitar "boletes"
                        size = min(1.0, 1.2 * star_scale)
                        a_val = 220 - min(100, int((mag - 5.0) * 15))

                        # Faint stars lose color saturation to the eye; pull them towards white/grey
                        desat = 0.5
                        r_desat = int(r_val * (1 - desat) + 200 * desat)
                        g_desat = int(g_val * (1 - desat) + 200 * desat)
                        b_desat = int(b_val * (1 - desat) + 220 * desat)

                        final_a = int(max(60, a_val) * alpha_f * 0.7)

                        painter.setBrush(
                            QColor(r_desat, g_desat, b_desat, final_a)
                        )
                        painter.drawEllipse(QPointF(x, y), size, size)
                    else:
                        # Estrellas principales
                        size = max(1.5, (5.0 - mag) * 0.7 * star_scale)
                        core_radius = min(4.0, size)

                        final_a = int(255 * alpha_f)
                        star_c_intense = QColor(r_val, g_val, b_val, final_a)
                        transparent_c = QColor(r_val, g_val, b_val, 0)

                        # 1. Halo molt més centrat i menys opac per evitar el
                        # "vel"
                        if mag < 4.0:
                            halo_size = (
                                core_radius * (5.5 - mag) * 1.8 * zoom_level
                            )
                            if mag < 1.0:
                                halo_size = core_radius * 8.0 * zoom_level

                            halo_grad = QRadialGradient(x, y, halo_size)
                            halo_grad.setColorAt(
                                0.0,
                                QColor(r_val, g_val, b_val, int(90 * alpha_f)),
                            )
                            halo_grad.setColorAt(
                                0.1,
                                QColor(r_val, g_val, b_val, int(30 * alpha_f)),
                            )
                            halo_grad.setColorAt(
                                0.3,
                                QColor(r_val, g_val, b_val, int(5 * alpha_f)),
                            )
                            halo_grad.setColorAt(1.0, transparent_c)

                            painter.setBrush(QBrush(halo_grad))
                            painter.drawEllipse(
                                QPointF(x, y), halo_size, halo_size
                            )

                        # 2. Nucli més brillant i dens
                        core_grad = QRadialGradient(x, y, core_radius)
                        core_grad.setColorAt(
                            0.0, QColor(255, 255, 255, final_a)
                        )
                        core_grad.setColorAt(
                            0.6, star_c_intense
                        )  # Manté el color base més cap a la vora
                        core_grad.setColorAt(1.0, transparent_c)

                        painter.setBrush(QBrush(core_grad))
                        painter.drawEllipse(
                            QPointF(x, y), core_radius, core_radius
                        )

                    # Diffraction Spikes for brightest stars (replicating draw_spikes logic)
                    # Use spike_threshold from params if available, otherwise default to 2.0
                    spike_threshold = params.get("spike_threshold", 2.0)
                    if mag < spike_threshold:
                        # Calculate factor: how much brighter than threshold?
                        factor = spike_threshold - mag

                        if factor > 0:
                            # Spike parameters (matching draw_spikes)
                            spike_length = factor * 20 * zoom_level
                            spike_width = max(0.5, factor * 0.4 * zoom_level)

                            if spike_length >= 4:
                                # Colors with gradient
                                spike_alpha = min(
                                    255, int(255 * alpha_f * 0.6)
                                )
                                c_center = QColor(
                                    int(f_r[i]),
                                    int(f_g[i]),
                                    int(f_b[i]),
                                    spike_alpha,
                                )
                                c_tip = QColor(
                                    int(f_r[i]), int(f_g[i]), int(f_b[i]), 0
                                )

                                painter.setPen(Qt.NoPen)

                                # Draw 4 spikes in cross pattern: 0Â°, 90Â°, 180Â°, 270Â°
                                for angle_deg in [0, 90, 180, 270]:
                                    painter.save()
                                    painter.translate(x, y)
                                    painter.rotate(angle_deg)

                                    # Gradient from center to tip
                                    grad = QLinearGradient(
                                        0, 0, spike_length, 0
                                    )
                                    grad.setColorAt(0.0, c_center)
                                    grad.setColorAt(
                                        0.05, c_center
                                    )  # Small bright core
                                    grad.setColorAt(1.0, c_tip)

                                    painter.setBrush(QBrush(grad))

                                    # Diamond-shaped ray
                                    path = QPainterPath()
                                    path.moveTo(0, -spike_width / 2.0)
                                    path.lineTo(spike_length, 0)
                                    path.lineTo(0, spike_width / 2.0)
                                    path.lineTo(-spike_width / 2.0, 0)
                                    path.closeSubpath()

                                    painter.drawPath(path)
                                    painter.restore()

            painter.end()
            self.result_ready.emit(img, visible_stars_out)

        except Exception as e:
            print(f"[StarRenderWorker] CRASH: {e}")
            import traceback

            traceback.print_exc()
            # Emit empty result to recover
            try:
                painter.end()
            except Exception:
                log_suppressed_exception(__name__, "StarRenderWorker.render")
            self.result_ready.emit(
                QImage(
                    params["width"],
                    params["height"],
                    QImage.Format_ARGB32_Premultiplied,
                ),
                [],
            )

    @pyqtSlot(dict)
    def render_trails(self, params):
        """Render star trails to a QImage in background thread."""
        if self._shutdown_event.is_set():
            return
        try:
            width = params["width"]
            height = params["height"]
            zoom_level = params["zoom_level"]
            vertical_ratio = params["vertical_ratio"]
            elevation_angle = params["elevation_angle"]
            azimuth_offset = params["azimuth_offset"]

            start_hour = params["start_hour"]
            end_hour = params["end_hour"]

            ra = params["ra"]
            dec = params["dec"]
            mag = params["mag"]
            r_arr = params["r"]
            g_arr = params["g"]
            b_arr = params["b"]

            lat_rad = params["lat_rad"]
            day_of_year = params["day_of_year"]
            longitude = params["longitude"]
            mag_limit = params["mag_limit"]

            # Time span
            diff = end_hour - start_hour
            if diff < -12.0:
                diff += 24.0
            elif diff > 12.0:
                diff -= 24.0

            if abs(diff) < 0.001:
                img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
                img.fill(Qt.transparent)
                self.trails_ready.emit(img)
                return

            # Determine is_moving
            is_moving = params.get("is_moving", False)

            if is_moving:
                # Low Quality: Only bright stars, few steps
                step_density = 1.0  # 1 step per hour
                mag_cap = 4.0  # Only stars brighter than mag 4
            else:
                # High Quality
                step_density = 4.0  # 4 steps per hour (15m)
                mag_cap = 5.5  # PERFORMANCE FIX: Valid only for bright stars

            n_steps = max(2, min(50, int(abs(diff) * step_density)))
            limit = min(mag_limit, mag_cap)

            # Create QImage
            img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            painter = QPainter(img)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(Qt.NoBrush)

            # Filter stars by magnitude
            mask = mag < limit + 1.0
            idxs = np.where(mask)[0]
            if len(idxs) == 0:
                painter.end()
                self.trails_ready.emit(img)
                return

            f_ra = ra[idxs]
            f_dec = dec[idxs]
            mag[idxs]
            f_r = r_arr[idxs]
            f_g = g_arr[idxs]
            f_b = b_arr[idxs]

            sin_lat = math.sin(lat_rad)
            cos_lat = math.cos(lat_rad)

            base_lst_0 = (100.0 + day_of_year * 0.9856 + longitude) % 360

            # Compute LSTs for each step
            step_lsts = []
            for i in range(n_steps + 1):
                t = float(i) / n_steps
                h = start_hour + diff * t
                lst = (base_lst_0 + h * 15.0) % 360
                step_lsts.append(lst)

            step_lsts = np.array(step_lsts)

            # Vectorized computation for ALL stars at ALL time steps
            dec_rad = np.radians(f_dec)[:, np.newaxis]
            sin_dec = np.sin(dec_rad)
            cos_dec = np.cos(dec_rad)

            ha = step_lsts[np.newaxis, :] - f_ra[:, np.newaxis]
            ha_rad = np.radians(ha)

            sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * np.cos(ha_rad)
            sin_alt = np.clip(sin_alt, -1.0, 1.0)
            alt_rad = np.arcsin(sin_alt)

            cos_alt = np.cos(alt_rad)
            cos_az_num = sin_dec - sin_alt * sin_lat
            cos_az_den = cos_alt * cos_lat + 1e-10
            cos_az = np.clip(cos_az_num / cos_az_den, -1.0, 1.0)
            az_rad = np.arccos(cos_az)
            sin_ha = np.sin(ha_rad)
            az_rad = np.where(sin_ha > 0, 2 * np.pi - az_rad, az_rad)

            # Projection constants
            # Projection - SPHERICAL STEREOGRAPHIC (matching render method)
            w, h = width, height
            scale_h = h / 2.0 * zoom_level
            cx = w / 2.0
            cy_base = h / 2.0 + (h * vertical_ratio)

            # View Shift
            elev_cam_rad = math.radians(elevation_angle)
            y_center_val = 2.0 * math.tan(elev_cam_rad / 2.0)

            # Camera azimuth
            cam_az_rad = math.radians(azimuth_offset)
            az_rel_rad = az_rad - cam_az_rad

            # Stereographic projection
            cos_alt = np.cos(alt_rad)
            sin_alt = np.sin(alt_rad)
            cos_az = np.cos(az_rel_rad)
            sin_az = np.sin(az_rel_rad)

            denom = 1.0 + cos_alt * cos_az

            # Invalid: Behind camera (denom near zero)
            invalid = denom < 1e-6

            # Projection formula (set invalid points to 0 temporarily)
            k = np.where(invalid, 0.0, 2.0 / denom)
            x = k * cos_alt * sin_az
            y = k * sin_alt

            cx + x * scale_h
            cy_base - (y - y_center_val) * scale_h

            jump_threshold = min(width, height) * 0.5

            # NumPy-heavy chunks run in threads, keeping canvas shutdown free
            # from child-process lifecycle races.
            import os

            n_stars = len(f_ra)
            n_workers = min(4, os.cpu_count() or 4)
            chunk_size = (n_stars + n_workers - 1) // n_workers

            tasks = []
            for i in range(0, n_stars, chunk_size):
                end_idx = min(i + chunk_size, n_stars)
                tasks.append(
                    (
                        f_ra[i:end_idx],
                        f_dec[i:end_idx],
                        f_r[i:end_idx],
                        f_g[i:end_idx],
                        f_b[i:end_idx],
                        step_lsts,
                        sin_lat,
                        cos_lat,
                        scale_h,
                        cx,
                        cy_base,
                        y_center_val,
                        cam_az_rad,
                        jump_threshold,
                        self._shutdown_event,
                    )
                )

            # Execute tasks in parallel
            with ThreadPoolExecutor(
                max_workers=n_workers,
                thread_name_prefix="star-trails",
            ) as executor:
                all_results = list(
                    executor.map(_compute_trail_segments_chunk, tasks)
                )

            # Draw aggregated results onto the pixel buffer
            for chunk_res in all_results:
                if self._shutdown_event.is_set():
                    painter.end()
                    return
                for (r, g, b), star_segments in chunk_res:
                    color = QColor(r, g, b, 120)
                    painter.setPen(QPen(color, 1.0))

                    path = QPainterPath()
                    for seg_x, seg_y in star_segments:
                        first = True
                        for px, py in zip(seg_x, seg_y):
                            if first:
                                path.moveTo(float(px), float(py))
                                first = False
                            else:
                                path.lineTo(float(px), float(py))
                    painter.drawPath(path)

            painter.end()
            self.trails_ready.emit(img)
        except Exception:
            log_suppressed_exception(__name__, "StarRenderWorker.render_trails")
