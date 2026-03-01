import math
# Test the new scaling logic

screen_width = 1920
zoom = 1.0

# Copy logic from _draw_single_city_dome
def test_dome(intensity, dist_m):
    dist_factor = math.exp(-dist_m / 35000.0) 
    log_intensity = math.log10(1.0 + intensity)
    visual_intensity = log_intensity * dist_factor
    
    alpha_base = min(100, int(visual_intensity * 60 * 1.0))
    alpha_base = int(alpha_base * 1.5)
    
    max_rad = screen_width * 0.4
    rad_x = min(max_rad, log_intensity * 30.0 * zoom * dist_factor)
    rad_y = rad_x * 0.35
    
    return alpha_base, rad_x, rad_y

peaks = [
    (195.47, 88000),
    (231.62, 84000),
    (117.43, 80000),
    (100.50, 72000),
    (88.54, 70000),
    (110.48, 52000),
    (379.71, 42000),
    (299.48, 42000),
    (165.35, 40000),
    (202.34, 24000)
]

for p in peaks:
    a, rx, ry = test_dome(p[0], p[1])
    print(f"Intensity {p[0]:.1f} @ {p[1]:.0f}m -> Alpha: {a}, RadX: {rx:.0f}px, RadY: {ry:.0f}px")
