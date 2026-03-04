import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen

from TerraLab.common.utils import getTraduction
from TerraLab.widgets.spherical_math import (
    angular_delta_signed,
    angular_distance,
    destination_point,
    screen_to_sky,
    slerp_arc_points,
)


SkyCoord = Tuple[float, float]  # (alt_deg, az_deg)

TOOL_NONE = "none"
TOOL_RULER = "ruler"
TOOL_SQUARE = "square"
TOOL_RECTANGLE = "rectangle"
TOOL_CIRCLE = "circle"

DRAG_NONE = "none"
DRAG_CREATING = "creating"
DRAG_MOVING = "moving"
DRAG_RESIZING = "resizing"


@dataclass
class MeasurementItem:
    tool: str
    a: SkyCoord
    b: SkyCoord
    # Rectangle-only hook: local-frame tilt in degrees.
    rotation_deg: float = 0.0


@dataclass
class RenderInfo:
    paths: List[List[SkyCoord]]
    label: str
    anchor: SkyCoord
    handles: Dict[str, SkyCoord]
    hit_polygon: Optional[List[SkyCoord]] = None


@dataclass
class MeasurementController:
    active_tool: str = TOOL_NONE
    current_start: Optional[SkyCoord] = None
    current_cursor: Optional[SkyCoord] = None
    ruler_first_point: Optional[SkyCoord] = None
    items: List[MeasurementItem] = field(default_factory=list)

    selected_index: Optional[int] = None
    drag_mode: str = DRAG_NONE
    resize_handle: Optional[str] = None
    last_drag_sky: Optional[SkyCoord] = None

    handle_hit_px: float = 12.0
    shape_hit_px: float = 8.0
    _undo_stack: List[dict] = field(default_factory=list)
    _max_undo_states: int = 256
    _drag_undo_pushed: bool = False

    @staticmethod
    def _clone_items(items: List[MeasurementItem]) -> List[MeasurementItem]:
        return [
            MeasurementItem(
                tool=str(it.tool),
                a=(float(it.a[0]), float(it.a[1])),
                b=(float(it.b[0]), float(it.b[1])),
                rotation_deg=float(it.rotation_deg),
            )
            for it in items
        ]

    def _snapshot_state(self) -> dict:
        return {
            "items": self._clone_items(self.items),
            "selected_index": self.selected_index,
            "current_start": tuple(self.current_start) if self.current_start is not None else None,
            "current_cursor": tuple(self.current_cursor) if self.current_cursor is not None else None,
            "ruler_first_point": tuple(self.ruler_first_point) if self.ruler_first_point is not None else None,
            "drag_mode": str(self.drag_mode),
            "resize_handle": self.resize_handle,
            "last_drag_sky": tuple(self.last_drag_sky) if self.last_drag_sky is not None else None,
            "active_tool": str(self.active_tool),
        }

    def _restore_snapshot(self, snap: dict) -> None:
        self.items = self._clone_items(list(snap.get("items", [])))
        self.selected_index = snap.get("selected_index")
        cs = snap.get("current_start")
        cc = snap.get("current_cursor")
        rf = snap.get("ruler_first_point")
        ld = snap.get("last_drag_sky")
        self.current_start = (float(cs[0]), float(cs[1])) if isinstance(cs, (tuple, list)) and len(cs) == 2 else None
        self.current_cursor = (float(cc[0]), float(cc[1])) if isinstance(cc, (tuple, list)) and len(cc) == 2 else None
        self.ruler_first_point = (float(rf[0]), float(rf[1])) if isinstance(rf, (tuple, list)) and len(rf) == 2 else None
        self.last_drag_sky = (float(ld[0]), float(ld[1])) if isinstance(ld, (tuple, list)) and len(ld) == 2 else None
        self.drag_mode = str(snap.get("drag_mode", DRAG_NONE))
        self.resize_handle = snap.get("resize_handle")
        self.active_tool = str(snap.get("active_tool", self.active_tool))
        self._drag_undo_pushed = False

    def _push_undo_state(self) -> None:
        snap = self._snapshot_state()
        if self._undo_stack and self._undo_stack[-1] == snap:
            return
        self._undo_stack.append(snap)
        if len(self._undo_stack) > int(self._max_undo_states):
            self._undo_stack = self._undo_stack[-int(self._max_undo_states) :]

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        snap = self._undo_stack.pop()
        self._restore_snapshot(snap)
        return True

    def set_tool(self, tool: str) -> None:
        self.active_tool = tool
        self.cancel_current()

    def clear(self) -> None:
        if self.items:
            self._push_undo_state()
        self.items.clear()
        self.cancel_current()
        self.selected_index = None

    def delete_selected(self) -> bool:
        if self.selected_index is None:
            return False
        if not (0 <= self.selected_index < len(self.items)):
            self.selected_index = None
            return False
        self._push_undo_state()
        del self.items[self.selected_index]
        self.selected_index = None
        self.drag_mode = DRAG_NONE
        self.resize_handle = None
        self.last_drag_sky = None
        self._drag_undo_pushed = False
        return True

    def cancel_current(self) -> None:
        self.current_start = None
        self.current_cursor = None
        self.drag_mode = DRAG_NONE
        self.resize_handle = None
        self.last_drag_sky = None
        if self.active_tool != TOOL_RULER:
            self.ruler_first_point = None

    def has_active_interaction(self) -> bool:
        return (
            self.current_start is not None
            or self.current_cursor is not None
            or self.ruler_first_point is not None
            or self.drag_mode != DRAG_NONE
        )

    def on_mouse_press(
        self,
        sx: float,
        sy: float,
        unproject_fn: Callable,
        project_fn: Optional[Callable[[float, float], Optional[Tuple[float, float]]]] = None,
    ) -> bool:
        if self.active_tool == TOOL_NONE:
            return False

        sky = screen_to_sky(sx, sy, unproject_fn)
        if sky is None:
            return True

        hit = self._pick_item(float(sx), float(sy), project_fn) if project_fn is not None else None
        if hit is not None:
            idx, handle = hit
            self.selected_index = idx
            self.current_start = None
            self.current_cursor = None
            self.ruler_first_point = None
            if handle is not None:
                self.drag_mode = DRAG_RESIZING
                self.resize_handle = handle
                self._drag_undo_pushed = False
            else:
                self.drag_mode = DRAG_MOVING
                self.last_drag_sky = sky
                self._drag_undo_pushed = False
            return True

        self.selected_index = None

        if self.active_tool == TOOL_RULER:
            if self.ruler_first_point is None:
                self.ruler_first_point = sky
                self.current_cursor = sky
            else:
                self._push_undo_state()
                it = MeasurementItem(tool=TOOL_RULER, a=self.ruler_first_point, b=sky)
                self.items.append(it)
                self.selected_index = len(self.items) - 1
                self.ruler_first_point = None
                self.current_cursor = None
            return True

        self.current_start = sky
        self.current_cursor = sky
        self.drag_mode = DRAG_CREATING
        self._drag_undo_pushed = False
        return True

    def on_mouse_move(
        self,
        sx: float,
        sy: float,
        unproject_fn: Callable,
        project_fn: Optional[Callable[[float, float], Optional[Tuple[float, float]]]] = None,
    ) -> bool:
        if self.active_tool == TOOL_NONE:
            return False

        sky = screen_to_sky(sx, sy, unproject_fn)
        if sky is None:
            return True

        if self.drag_mode == DRAG_CREATING:
            self.current_cursor = sky
            return True

        if self.drag_mode == DRAG_MOVING and self.selected_index is not None and self.last_drag_sky is not None:
            if not self._drag_undo_pushed:
                self._push_undo_state()
                self._drag_undo_pushed = True
            d_alt = sky[0] - self.last_drag_sky[0]
            d_az = angular_delta_signed(self.last_drag_sky[1], sky[1])
            self._translate_item(self.selected_index, d_alt, d_az)
            self.last_drag_sky = sky
            return True

        if self.drag_mode == DRAG_RESIZING and self.selected_index is not None and self.resize_handle:
            if not self._drag_undo_pushed:
                self._push_undo_state()
                self._drag_undo_pushed = True
            self._resize_item(self.selected_index, self.resize_handle, sky)
            return True

        if self.active_tool == TOOL_RULER and self.ruler_first_point is not None:
            self.current_cursor = sky
            return True

        return False

    def on_mouse_release(
        self,
        sx: float,
        sy: float,
        unproject_fn: Callable,
        project_fn: Optional[Callable[[float, float], Optional[Tuple[float, float]]]] = None,
    ) -> bool:
        if self.active_tool == TOOL_NONE:
            return False

        sky = screen_to_sky(sx, sy, unproject_fn)
        if sky is not None and self.drag_mode == DRAG_CREATING:
            self.current_cursor = sky

        if self.drag_mode == DRAG_CREATING and self.current_start is not None and self.current_cursor is not None:
            self._push_undo_state()
            it = MeasurementItem(tool=self.active_tool, a=self.current_start, b=self.current_cursor)
            self.items.append(it)
            self.selected_index = len(self.items) - 1
            self.current_start = None
            self.current_cursor = None

        consumed = self.drag_mode != DRAG_NONE
        self.drag_mode = DRAG_NONE
        self.resize_handle = None
        self.last_drag_sky = None
        self._drag_undo_pushed = False
        return consumed

    def draw(
        self,
        painter: QPainter,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        formatters: Dict[str, Callable[[float], str]],
    ) -> None:
        if (
            not self.items
            and self.current_start is None
            and self.ruler_first_point is None
        ):
            return

        for i, it in enumerate(self.items):
            ri = self._render_item(it)
            self._draw_render_info(painter, project_fn, ri, selected=(i == self.selected_index))

        if self.active_tool == TOOL_RULER and self.ruler_first_point is not None and self.current_cursor is not None:
            preview = self._render_item(MeasurementItem(tool=TOOL_RULER, a=self.ruler_first_point, b=self.current_cursor))
            self._draw_render_info(painter, project_fn, preview, selected=False, preview_alpha=140)

        if self.active_tool in (TOOL_SQUARE, TOOL_RECTANGLE, TOOL_CIRCLE):
            if self.current_start is not None and self.current_cursor is not None:
                preview = self._render_item(MeasurementItem(tool=self.active_tool, a=self.current_start, b=self.current_cursor))
                self._draw_render_info(painter, project_fn, preview, selected=False, preview_alpha=140)

        if self.active_tool == TOOL_RULER and self.ruler_first_point is not None:
            p = project_fn(*self.ruler_first_point)
            if p is not None:
                painter.save()
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.setPen(QPen(QColor(255, 255, 255, 230), 1.2))
                painter.setBrush(QColor(255, 255, 255, 110))
                painter.drawEllipse(QPointF(float(p[0]), float(p[1])), 3.5, 3.5)
                painter.restore()

    def update_preview_cursor(self, sx: float, sy: float, unproject_fn: Callable) -> None:
        if self.active_tool == TOOL_RULER and self.ruler_first_point is not None:
            sky = screen_to_sky(sx, sy, unproject_fn)
            if sky is not None:
                self.current_cursor = sky

    def _draw_render_info(
        self,
        painter: QPainter,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        ri: RenderInfo,
        selected: bool,
        preview_alpha: int = 220,
    ) -> None:
        for sky_path in ri.paths:
            pts = self._project_path(project_fn, sky_path)
            if len(pts) < 2:
                continue
            self._stroke_segment(painter, pts, preview_alpha, selected=selected)

        anc = project_fn(*ri.anchor)
        if anc is not None:
            self._draw_label(painter, float(anc[0]), float(anc[1]), ri.label, preview_alpha)

        if selected:
            self._draw_handles(painter, project_fn, ri, preview_alpha)

    def _stroke_segment(self, painter: QPainter, pts: List[QPointF], alpha: int, selected: bool = False) -> None:
        if len(pts) < 2:
            return
        path = QPainterPath()
        path.moveTo(pts[0])
        for p in pts[1:]:
            path.lineTo(p)

        glow = QColor(255, 255, 180, int(alpha * 0.45)) if selected else QColor(255, 255, 255, int(alpha * 0.35))
        line = QColor(255, 245, 120, alpha) if selected else QColor(255, 255, 255, alpha)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(glow, 3.2 if selected else 3.0))
        painter.drawPath(path)
        painter.setPen(QPen(line, 1.2 if selected else 1.0))
        painter.drawPath(path)
        painter.restore()

    def _draw_handles(
        self,
        painter: QPainter,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        ri: RenderInfo,
        alpha: int,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        for _, hsky in ri.handles.items():
            p = project_fn(*hsky)
            if p is None:
                continue
            q = QPointF(float(p[0]), float(p[1]))
            painter.setPen(QPen(QColor(255, 240, 120, alpha), 1.1))
            painter.setBrush(QColor(0, 0, 0, min(200, alpha)))
            painter.drawEllipse(q, 4.0, 4.0)
        painter.restore()

    def _draw_label(self, painter: QPainter, x: float, y: float, txt: str, alpha: int) -> None:
        painter.save()
        # Mida del HUD ajustada al text real per evitar caixes sobredimensionades.
        lines = [line for line in str(txt).split("\n") if line != ""]
        if not lines:
            lines = [""]
        fm = painter.fontMetrics()
        line_h = float(fm.lineSpacing())
        max_line_w = max(float(fm.horizontalAdvance(line)) for line in lines)

        pad_x = 6.0
        pad_y = 4.0
        content_w = max_line_w
        content_h = line_h * len(lines)
        box_w = min(320.0, max(1.0, content_w + 2.0 * pad_x))
        box_h = max(1.0, content_h + 2.0 * pad_y)
        rect = QRectF(x + 8.0, y + 8.0, box_w, box_h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, min(205, alpha)))
        painter.drawRoundedRect(rect, 5.0, 5.0)
        painter.setPen(QColor(255, 255, 255, alpha))
        text_x = rect.left() + pad_x
        text_y = rect.top() + pad_y
        for i, line in enumerate(lines):
            y_baseline = text_y + line_h * i + fm.ascent()
            painter.drawText(QPointF(text_x, y_baseline), line)
        painter.restore()

    def _pick_item(
        self,
        sx: float,
        sy: float,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
    ) -> Optional[Tuple[int, Optional[str]]]:
        for idx in range(len(self.items) - 1, -1, -1):
            it = self.items[idx]
            ri = self._render_item(it)

            # Handles first (resize)
            for hk, hsky in ri.handles.items():
                hp = project_fn(*hsky)
                if hp is None:
                    continue
                if self._dist(sx, sy, float(hp[0]), float(hp[1])) <= self.handle_hit_px:
                    return idx, hk

            # Body hit (move/select)
            for sky_path in ri.paths:
                pth = self._project_path(project_fn, sky_path)
                if len(pth) < 2:
                    continue
                d = self._point_to_polyline_dist(sx, sy, pth)
                if d <= self.shape_hit_px:
                    return idx, None

            if ri.hit_polygon:
                poly = self._project_path(project_fn, ri.hit_polygon)
                if len(poly) >= 3 and self._point_in_polygon(sx, sy, poly):
                    return idx, None

        return None

    def _translate_item(self, idx: int, d_alt: float, d_az: float) -> None:
        if not (0 <= idx < len(self.items)):
            return
        it = self.items[idx]
        it.a = self._shift(it.a, d_alt, d_az)
        it.b = self._shift(it.b, d_alt, d_az)

    def _resize_item(self, idx: int, handle: str, sky: SkyCoord) -> None:
        if not (0 <= idx < len(self.items)):
            return
        it = self.items[idx]
        if it.tool == TOOL_RULER:
            if handle == "a":
                it.a = self._norm(sky)
            elif handle == "b":
                it.b = self._norm(sky)
            return

        if it.tool == TOOL_CIRCLE:
            if handle == "edge":
                it.b = self._norm(sky)
            elif handle == "center":
                # Move center while preserving current radius vector roughly.
                d_alt = sky[0] - it.a[0]
                d_az = angular_delta_signed(it.a[1], sky[1])
                it.a = self._shift(it.a, d_alt, d_az)
                it.b = self._shift(it.b, d_alt, d_az)
            return

        # Rectangle / square
        if handle in ("origin", "corner"):
            self._resize_rect_from_corner(it, handle, self._norm(sky), force_square=(it.tool == TOOL_SQUARE))
        elif handle == "rotate" and it.tool == TOOL_RECTANGLE:
            center, _, _, _, sign_alt, _ = self._rect_params(it, force_square=False)
            x, y = self._sky_to_local(center, self._norm(sky))
            if abs(x) < 1e-6 and abs(y) < 1e-6:
                return
            # Rotation is measured from the local "up" axis of the rectangle.
            base_ang = math.atan2(sign_alt, 0.0)
            raw_ang = math.atan2(y, x)
            rot_deg = math.degrees(raw_ang - base_ang)
            it.rotation_deg = ((rot_deg + 180.0) % 360.0) - 180.0

    def _resize_rect_from_corner(
        self,
        it: MeasurementItem,
        handle: str,
        dragged: SkyCoord,
        force_square: bool,
    ) -> None:
        """
        Corner resize based on current geometry, not from scratch.
        Keeps the opposite corner fixed, preserves current rotation and
        updates width/height (or side for square) from the live drag.
        """
        center, width, height, sign_az, sign_alt, rot_deg = self._rect_params(it, force_square=force_square)
        p00, _, p11, _, _, _ = self._rect_corners(
            center=center,
            width=width,
            height=height,
            sign_az=sign_az,
            sign_alt=sign_alt,
            rot_deg=rot_deg,
        )
        fixed = p11 if handle == "origin" else p00

        # First-order midpoint on local tangent frame.
        d_alt = dragged[0] - fixed[0]
        d_az = angular_delta_signed(fixed[1], dragged[1])
        center_guess = self._norm((fixed[0] + 0.5 * d_alt, fixed[1] + 0.5 * d_az))

        rot_rad = math.radians(rot_deg)
        fx, fy = self._sky_to_local(center_guess, fixed)
        dx, dy = self._sky_to_local(center_guess, dragged)
        fx_u, fy_u = self._rotate_local(fx, fy, -rot_rad)
        dx_u, dy_u = self._rotate_local(dx, dy, -rot_rad)

        full_w = max(1e-6, abs(dx_u - fx_u))
        full_h = max(1e-6, abs(dy_u - fy_u))
        if force_square:
            side = max(full_w, full_h)
            full_w = side
            full_h = side

        sign_az_new = 1.0 if (dx_u - fx_u) >= 0.0 else -1.0
        sign_alt_new = 1.0 if (dy_u - fy_u) >= 0.0 else -1.0

        cx_u = 0.5 * (fx_u + dx_u)
        cy_u = 0.5 * (fy_u + dy_u)
        cx, cy = self._rotate_local(cx_u, cy_u, rot_rad)
        center_new = self._local_to_sky(center_guess, cx, cy)

        self._set_rect_from_params(
            it=it,
            center=center_new,
            width=full_w,
            height=full_h,
            sign_az=sign_az_new,
            sign_alt=sign_alt_new,
        )

    def _set_rect_from_params(
        self,
        it: MeasurementItem,
        center: SkyCoord,
        width: float,
        height: float,
        sign_az: float,
        sign_alt: float,
    ) -> None:
        width = max(1e-6, float(width))
        height = max(1e-6, float(height))
        c_alt, c_az = self._norm(center)
        half_h = 0.5 * height
        alt0 = c_alt - sign_alt * half_h
        alt1 = c_alt + sign_alt * half_h
        cos_lat = max(0.05, math.cos(math.radians(c_alt)))
        half_daz = (0.5 * width) / cos_lat
        az0 = c_az - sign_az * half_daz
        az1 = c_az + sign_az * half_daz
        it.a = self._norm((alt0, az0))
        it.b = self._norm((alt1, az1))

    def _render_item(self, it: MeasurementItem) -> RenderInfo:
        if it.tool == TOOL_RULER:
            return self._render_ruler(it)
        if it.tool == TOOL_CIRCLE:
            return self._render_circle(it)
        if it.tool == TOOL_SQUARE:
            return self._render_rect_like(it, force_square=True)
        if it.tool == TOOL_RECTANGLE:
            return self._render_rect_like(it, force_square=False)

        # Fallback
        return RenderInfo(paths=[], label="", anchor=it.a, handles={})

    def _render_ruler(self, it: MeasurementItem) -> RenderInfo:
        dist = angular_distance(it.a, it.b)
        arc = slerp_arc_points(it.a, it.b, n_points=72)
        lbl = getTraduction("Measure.Distance", "Distance: {v:.3f}deg").format(v=dist)
        mid = arc[len(arc) // 2]
        return RenderInfo(
            paths=[arc],
            label=lbl,
            anchor=mid,
            handles={"a": it.a, "b": it.b},
            hit_polygon=None,
        )

    def _render_circle(self, it: MeasurementItem) -> RenderInfo:
        center = self._norm(it.a)
        edge = self._norm(it.b)
        r = max(1e-6, angular_distance(center, edge))
        dia = 2.0 * r
        # Small-angle approximation in square degrees; hook for exact spherical cap area.
        area = math.pi * (dia * 0.5) * (dia * 0.5)

        pts: List[SkyCoord] = []
        steps = 128
        for i in range(steps + 1):
            b = 360.0 * i / steps
            pts.append(destination_point(center, b, r))

        lbl = (
            getTraduction("Measure.Diameter", "Diameter: {v:.3f}deg").format(v=dia)
            + " | "
            + getTraduction("Measure.Area", "Area: {v:.3f} deg2").format(v=area)
        )
        anc = destination_point(center, 45.0, max(r * 0.65, 0.2))
        return RenderInfo(
            paths=[pts],
            label=lbl,
            anchor=anc,
            handles={"center": center, "edge": edge},
            hit_polygon=pts,
        )

    def _render_rect_like(self, it: MeasurementItem, force_square: bool) -> RenderInfo:
        center, width, height, sign_az, sign_alt, rot_deg = self._rect_params(it, force_square=force_square)
        p00, p10, p11, p01, top_mid, rotate_handle = self._rect_corners(
            center=center,
            width=width,
            height=height,
            sign_az=sign_az,
            sign_alt=sign_alt,
            rot_deg=rot_deg,
        )

        e1 = slerp_arc_points(p00, p10, 24)
        e2 = slerp_arc_points(p10, p11, 24)
        e3 = slerp_arc_points(p11, p01, 24)
        e4 = slerp_arc_points(p01, p00, 24)

        poly = e1 + e2[1:] + e3[1:] + e4[1:]
        # Small-angle approximation in square degrees (sufficient for local framing tools).
        area = width * height
        lbl = (
            getTraduction("Measure.Width", "Width: {v:.3f}deg").format(v=width)
            + "\n"
            + getTraduction("Measure.Height", "Height: {v:.3f}deg").format(v=height)
            + "\n"
            + getTraduction("Measure.Area", "Area: {v:.3f} deg2").format(v=area)
        )
        anc = center
        handles = {"origin": p00, "corner": p11}
        paths = [poly]
        if it.tool == TOOL_RECTANGLE and not force_square:
            handles["rotate"] = rotate_handle
            paths.append(slerp_arc_points(top_mid, rotate_handle, 8))
        return RenderInfo(
            paths=paths,
            label=lbl,
            anchor=anc,
            handles=handles,
            hit_polygon=poly,
        )

    def _rect_params(
        self,
        it: MeasurementItem,
        force_square: bool,
    ) -> Tuple[SkyCoord, float, float, float, float, float]:
        a = self._norm(it.a)
        b = self._norm(it.b)

        alt0, az0 = a
        alt1, az1 = b
        width_est = angular_distance((alt0, az0), (alt0, az1))
        height_est = angular_distance((alt0, az0), (alt1, az0))

        sign_alt = 1.0 if (alt1 - alt0) >= 0.0 else -1.0
        sign_az = 1.0 if angular_delta_signed(az0, az1) >= 0.0 else -1.0

        if force_square:
            side = max(width_est, height_est)
            width = side
            height = side
        else:
            width = width_est
            height = height_est

        width = max(1e-6, width)
        height = max(1e-6, height)
        center_alt = max(-89.9, min(89.9, (alt0 + alt1) * 0.5))
        center_az = (az0 + angular_delta_signed(az0, az1) * 0.5) % 360.0
        rot_deg = float(it.rotation_deg) if (it.tool == TOOL_RECTANGLE and not force_square) else 0.0
        return (center_alt, center_az), width, height, sign_az, sign_alt, rot_deg

    def _rect_corners(
        self,
        center: SkyCoord,
        width: float,
        height: float,
        sign_az: float,
        sign_alt: float,
        rot_deg: float,
    ) -> Tuple[SkyCoord, SkyCoord, SkyCoord, SkyCoord, SkyCoord, SkyCoord]:
        half_w = max(1e-6, width * 0.5)
        half_h = max(1e-6, height * 0.5)
        rot_rad = math.radians(rot_deg)

        base = [
            (-half_w, -half_h),
            (half_w, -half_h),
            (half_w, half_h),
            (-half_w, half_h),
        ]
        local_corners: List[Tuple[float, float]] = []
        for x, y in base:
            xr, yr = self._rotate_local(x * sign_az, y * sign_alt, rot_rad)
            local_corners.append((xr, yr))

        p00 = self._local_to_sky(center, local_corners[0][0], local_corners[0][1])
        p10 = self._local_to_sky(center, local_corners[1][0], local_corners[1][1])
        p11 = self._local_to_sky(center, local_corners[2][0], local_corners[2][1])
        p01 = self._local_to_sky(center, local_corners[3][0], local_corners[3][1])

        # Rotation control handle is offset from the "top" side midpoint in local frame.
        top_mid_x, top_mid_y = self._rotate_local(0.0, sign_alt * half_h, rot_rad)
        rotate_dist = half_h + max(0.15, 0.15 * max(width, height))
        rot_h_x, rot_h_y = self._rotate_local(0.0, sign_alt * rotate_dist, rot_rad)
        top_mid = self._local_to_sky(center, top_mid_x, top_mid_y)
        rotate_handle = self._local_to_sky(center, rot_h_x, rot_h_y)
        return p00, p10, p11, p01, top_mid, rotate_handle

    def _project_path(
        self,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        sky_path: List[SkyCoord],
    ) -> List[QPointF]:
        out: List[QPointF] = []
        for alt, az in sky_path:
            p = project_fn(alt, az)
            if p is None:
                continue
            out.append(QPointF(float(p[0]), float(p[1])))
        return out

    @staticmethod
    def _dist(x1: float, y1: float, x2: float, y2: float) -> float:
        dx = x1 - x2
        dy = y1 - y2
        return math.sqrt(dx * dx + dy * dy)

    def _point_to_polyline_dist(self, x: float, y: float, pts: List[QPointF]) -> float:
        best = 1e9
        for i in range(len(pts) - 1):
            d = self._point_to_segment_dist(x, y, pts[i], pts[i + 1])
            if d < best:
                best = d
        return best

    def _point_to_segment_dist(self, x: float, y: float, a: QPointF, b: QPointF) -> float:
        ax, ay = a.x(), a.y()
        bx, by = b.x(), b.y()
        vx = bx - ax
        vy = by - ay
        wx = x - ax
        wy = y - ay
        vv = vx * vx + vy * vy
        if vv < 1e-9:
            return self._dist(x, y, ax, ay)
        t = max(0.0, min(1.0, (wx * vx + wy * vy) / vv))
        px = ax + t * vx
        py = ay + t * vy
        return self._dist(x, y, px, py)

    def _point_in_polygon(self, x: float, y: float, poly: List[QPointF]) -> bool:
        inside = False
        n = len(poly)
        if n < 3:
            return False
        j = n - 1
        for i in range(n):
            xi, yi = poly[i].x(), poly[i].y()
            xj, yj = poly[j].x(), poly[j].y()
            intersects = ((yi > y) != (yj > y)) and (
                x < (xj - xi) * (y - yi) / max(1e-9, (yj - yi)) + xi
            )
            if intersects:
                inside = not inside
            j = i
        return inside

    def _norm(self, p: SkyCoord) -> SkyCoord:
        alt = max(-89.9, min(89.9, float(p[0])))
        az = float(p[1]) % 360.0
        return alt, az

    def _shift(self, p: SkyCoord, d_alt: float, d_az: float) -> SkyCoord:
        return self._norm((p[0] + d_alt, p[1] + d_az))

    @staticmethod
    def _rotate_local(x: float, y: float, angle_rad: float) -> Tuple[float, float]:
        ca = math.cos(angle_rad)
        sa = math.sin(angle_rad)
        return (x * ca - y * sa, x * sa + y * ca)

    def _local_to_sky(self, center: SkyCoord, x_deg: float, y_deg: float) -> SkyCoord:
        c_alt, c_az = center
        cos_lat = max(0.05, math.cos(math.radians(c_alt)))
        alt = c_alt + y_deg
        az = c_az + (x_deg / cos_lat)
        return self._norm((alt, az))

    def _sky_to_local(self, center: SkyCoord, sky: SkyCoord) -> Tuple[float, float]:
        c_alt, c_az = center
        alt, az = self._norm(sky)
        cos_lat = max(0.05, math.cos(math.radians(c_alt)))
        x = angular_delta_signed(c_az, az) * cos_lat
        y = alt - c_alt
        return x, y
