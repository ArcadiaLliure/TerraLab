import math


class AtmosphericMath:
    """
    Utilitats de fisica atmosferica.

    Variables principals del modul (no totes s'usen a cada funcio):
    - h_deg: alcada aparent de l'objecte sobre l'horitzo [graus].
    - X: massa d'aire (airmass), cami optic relatiu dins l'atmosfera.
    - AOD: aerosol optical depth (font externa de qualitat de l'aire), adimensional.
    - P_hPa: pressio superficial [hPa] (font meteorologica externa).
    - k: coeficient d'extincio [mag/airmass].
    - loss_mag: perdua total de magnitud per atmosfera [mag].
    - T: transmissio relativa [0..1].
    """

    @staticmethod
    def airmass_from_altitude_deg(h_deg):
        """
        Calcula la massa d'aire X a partir de l'alcada aparent.

        Variables utilitzades en aquesta funcio:
        - entrada: h_deg
        - sortida: X

        Regim utilitzat:
        - h >= 30 deg: aproximacio secant: X = 1/sin(h)
        - h < 30 deg: Kasten & Young (1989), mes estable prop de l'horitzo

        Retorn:
        - float: X si h_deg > 0
        - None: si l'objecte es sota l'horitzo (h_deg <= 0)
        """
        if h_deg is None or h_deg <= 0.0:
            return None
        if h_deg >= 30.0:
            return 1.0 / max(1e-6, math.sin(math.radians(h_deg)))
        h_rad = math.radians(h_deg)
        return 1.0 / (math.sin(h_rad) + 0.50572 * ((h_deg + 6.07995) ** (-1.6364)))

    @staticmethod
    def extinction_k_mag_per_airmass(aod, pressure_hpa, k_fallback=0.20):
        """
        Estima k (mag/airmass) a ~550 nm.

        Formules:
        - tau_aer = AOD
        - tau_R = 0.00879 * (P/1013.25)
        - k = 1.086 * (tau_aer + tau_R)

        Fallback:
        - si no hi ha dades valides d'AOD o pressio, retorna k_fallback.
        """
        if aod is None or pressure_hpa is None:
            return float(k_fallback)
        tau_aer = max(0.0, float(aod))
        tau_r = 0.00879 * (float(pressure_hpa) / 1013.25)
        return 1.086 * (tau_aer + tau_r)

    @staticmethod
    def loss_mag_from_k_airmass(extinction_k_mag_airmass, airmass_x):
        """
        Calcula la perdua atmosferica total en magnitud.

        Formula:
        - loss_mag = k * X
        """
        if airmass_x is None:
            return None
        return float(extinction_k_mag_airmass) * float(airmass_x)

    @staticmethod
    def transmission_from_loss_mag(loss_mag):
        """
        Converteix perdua en magnitud a transmissio relativa.

        Formula:
        - T = 10^(-0.4 * loss_mag)
        """
        if loss_mag is None:
            return None
        return 10.0 ** (-0.4 * float(loss_mag))


class InstrumentOpticsMath:
    """
    Utilitats optiques de l'instrument (telescopi/camera).

    Variables principals:
    - D_mm: obertura efectiva [mm].
    - F_tub_mm: focal del tub/lent [mm].
    - F_ocular_mm: focal d'ocular [mm] (nomes visual telescopic).
    - M: magnificacio visual.
    - p_sortida_mm: pupilla de sortida [mm].
    - p_ull_mm: pupilla de l'ull [mm].
    """

    @staticmethod
    def is_camera_profile(instrument_profile: str) -> bool:
        """
        Determina si el perfil es de camera.

        Perfils de camera actuals:
        - camera_aps_c
        - camera_full_frame
        """
        return str(instrument_profile) in ("camera_aps_c", "camera_full_frame")

    @staticmethod
    def magnification(telescope_focal_mm, eyepiece_focal_mm, is_camera):
        """
        Calcula la magnificacio visual.

        Formula telescopica:
        - M = F_tub / F_ocular

        En camera:
        - no hi ha ocular visual, retorn 1.0 per mantenir pipeline estable.
        """
        if is_camera:
            return 1.0
        ratio = float(telescope_focal_mm) / max(0.5, float(eyepiece_focal_mm))
        return max(0.1, ratio)

    @staticmethod
    def exit_pupil_mm(aperture_mm, magnification, is_camera):
        """
        Calcula la pupilla de sortida.

        Formula telescopica:
        - p_sortida = D / M

        En camera:
        - no aplica pupilla visual; retornem D per coherencia interna.
        """
        if is_camera:
            return max(1.0, float(aperture_mm))
        return max(1e-6, float(aperture_mm) / max(1e-6, float(magnification)))

    @staticmethod
    def effective_aperture_mm(aperture_mm, exit_pupil_mm, eye_pupil_mm, is_camera):
        """
        Calcula obertura efectiva visual.

        En telescopi visual:
        - si p_sortida > p_ull, part de la llum no entra a l'ull i l'obertura
          efectiva baixa per un factor p_ull/p_sortida.

        En camera:
        - no hi ha retall per pupilla humana.
        """
        aperture_mm = max(1.0, float(aperture_mm))
        if is_camera:
            return aperture_mm

        effective_aperture_mm = aperture_mm
        if exit_pupil_mm > eye_pupil_mm:
            effective_aperture_mm *= float(eye_pupil_mm) / max(1e-6, float(exit_pupil_mm))
        return max(0.1, effective_aperture_mm)

    @staticmethod
    def aperture_gain_mag(effective_aperture_mm, eye_pupil_mm):
        """
        Guany de magnitud per area collectora.

        Formula:
        - m_gain = 5*log10(D_eff / D_ull)
        """
        return 5.0 * math.log10(max(0.1, float(effective_aperture_mm)) / max(0.5, float(eye_pupil_mm)))


class VisualPhotometryMath:
    """
    Utilitats fotometriques del model de magnitud limit visual.
    """

    @staticmethod
    def clamp(value, low, high):
        """
        Satura un valor numeric dins un rang [low, high].
        """
        return max(low, min(high, float(value)))

    @staticmethod
    def bortle_to_nelm_mag(bortle_class):
        """
        Converteix classe Bortle a NELM aproximada (magnitud limit a ull nu).

        Corba de referencia usada:
        - B1 ~ 7.6
        - B9 ~ 3.6
        """
        bortle_class = VisualPhotometryMath.clamp(bortle_class, 1.0, 9.0)
        return 7.6 - 0.5 * (bortle_class - 1.0)

    @staticmethod
    def eye_limit_mag(auto_bortle, bortle_class, manual_eye_limit_mag):
        """
        Dona m_lim de base (ull nu).

        - auto_bortle=True: usa conversio Bortle->NELM
        - auto_bortle=False: usa valor manual de l'usuari
        """
        if auto_bortle:
            return VisualPhotometryMath.bortle_to_nelm_mag(float(bortle_class))
        return float(manual_eye_limit_mag)

    @staticmethod
    def atmospheric_loss_mag(value):
        """
        Normalitza perdua atmosferica a valor no negatiu.
        """
        return max(0.0, float(value))

    @staticmethod
    def overmagnification_penalty_mag(exit_pupil_mm, is_camera):
        """
        Penalitzacio visual per sobremagnificacio.

        En visual telescopic:
        - pupilles de sortida molt petites (<0.7 mm) penalitzen deteccio.

        En camera:
        - no aplica aquesta penalitzacio visual.
        """
        if is_camera:
            return 0.0
        return max(0.0, (0.7 - float(exit_pupil_mm)) * 0.35)

    @staticmethod
    def exposure_gain_mag(exposure_seconds, iso):
        """
        Guany de profunditat per exposicio/ISO.

        Model simplificat:
        - gain_mag ~ 1.25 * log10((t * ISO)/100)
        """
        ratio_vs_reference = max(1e-4, float(exposure_seconds) * max(1.0, float(iso)) / 100.0)
        gain_mag = 1.25 * math.log10(ratio_vs_reference)
        return VisualPhotometryMath.clamp(gain_mag, -3.0, 8.0)

    @staticmethod
    def sensor_bonus_mag(instrument_profile, sensor_profile, is_camera):
        """
        Ajust empiric de calibracio entre sensors.

        Nota:
        - no es una radiometria completa del sensor.
        - es un offset de calibracio visual del render.
        """
        if not is_camera:
            return 0.0
        if instrument_profile == "camera_full_frame" or sensor_profile == "full_frame":
            return 0.35
        return 0.15

    @staticmethod
    def scope_limit_mag(
        eye_limit_mag,
        aperture_gain_mag,
        atmospheric_loss_mag,
        overmagnification_penalty_mag,
        exposure_gain_mag,
        sensor_bonus_mag,
    ):
        """
        Magnitud limit final de l'instrument.

        Formula:
        m_lim_scope =
            m_lim_eye
            + guany_obertura
            - perdues_atmosfera
            - penalitzacio_sobremagnificacio
            + guany_exposicio
            + bonus_sensor
        """
        result = (
            float(eye_limit_mag)
            + float(aperture_gain_mag)
            - float(atmospheric_loss_mag)
            - float(overmagnification_penalty_mag)
            + float(exposure_gain_mag)
            + float(sensor_bonus_mag)
        )
        # El limit superior de 12.0 era massa restrictiu per a configuracions
        # de camera/exposicio alta, i feia que ISO/temps no tinguessin efecte
        # visible un cop assolit el sostre.
        return VisualPhotometryMath.clamp(result, -12.0, 18.0)

    @staticmethod
    def star_scale_factor(scope_limit_mag, eye_limit_mag, exposure_gain_mag):
        """
        Factor visual de representacio d'estrelles al render.

        No es magnitud fisica directa; es una transferencia visual:
        - si m_lim_scope puja respecte m_lim_eye, augmenta presencia d'estrelles
        - mes guany d'exposicio reforca lleugerament aquest efecte
        """
        depth_bonus_mag = max(0.0, float(scope_limit_mag) - float(eye_limit_mag))
        # A la mira telescopica, l'escala visual de les estrelles ha de reflectir
        # millor el guany fotometric per obertura+exposicio.
        factor = 0.90 + 0.08 * depth_bonus_mag + 0.08 * max(0.0, float(exposure_gain_mag))
        return VisualPhotometryMath.clamp(factor, 0.70, 3.50)

    @staticmethod
    def general_render_limit_mag(bortle_class, render_compensation_mag=0.0):
        """
        Retorna:
        - mlim_render: limit usat al render
        - mlim_physical: limit fisic base (NELM)

        render_compensation_mag:
        - compensa penalitzacions visuals extra del pipeline de render.
        - si es prioritza realisme fisic pur, ha de ser 0.0.
        """
        mlim_physical = VisualPhotometryMath.bortle_to_nelm_mag(bortle_class)
        mlim_render = mlim_physical + float(render_compensation_mag)
        return mlim_render, mlim_physical
