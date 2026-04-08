import numpy as np
import requests
import ezdxf
import io
import rasterio
import xml.etree.ElementTree as ET
from matplotlib import pyplot as plt
from scipy.ndimage import gaussian_filter
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union
from pyproj import Transformer

# --- КОНФІГУРАЦІЯ ---
API_KEY = 'c0b06f70c609069167ef0cf1384e796f'
KML_FILENAME = 'map.kml'
CONTOUR_INTERVAL = 5 # висота перетину рельєфу
SURFACE_SMOOTHING = 3  # Сила згладжування рельєфу (sigma)
USER_SCALE = 0.08       # 0.1 тоді 10 метрів = 1 мм у DXF (масштаб 1:10000)
MARGIN = 5              # Відступ між деталями (мм)
MAX_ROW_WIDTH = 5000     # Ширина листа картону (мм)
TARGET_EPSG = "EPSG:32637" 

# --- НОВИЙ ПАРАМЕТР ---
LABEL_HEIGHT = 5         # Розмір тексту підпису висоти в міліметрах
# ----------------------

def parse_kml(filename):
    print(f"📖 Читання координат з {filename}...")
    tree = ET.parse(filename)
    root = tree.getroot()
    ns = {'kml': 'http://www.opengis.net/kml/2.2'}
    coords_element = root.find('.//kml:coordinates', ns)
    if coords_element is None:
        raise ValueError("Не знайдено тег <coordinates> у файлі KML.")
    coords_text = coords_element.text.strip()
    points = [tuple(map(float, c.split(',')[:2])) for c in coords_text.split()]
    lons, lats = zip(*points)
    return min(lons), min(lats), max(lons), max(lats)

def download_dem(w, s, e, n):
    print("🌐 Завантаження рельєфу (Copernicus GLO-30)...")
    url = f"https://portal.opentopography.org/API/globaldem?demtype=COP30&west={w}&south={s}&east={e}&north={n}&outputFormat=GTiff&API_Key={API_KEY}"
    r = requests.get(url, timeout=60)
    if r.status_code != 200:
        raise Exception(f"Помилка API OpenTopography: {r.text}")
    
    with rasterio.open(io.BytesIO(r.content)) as src:
        data = src.read(1).astype(float)
        affine = src.transform
        data[data < -100] = np.nanmin(data[data > -100])
        return data, affine

def process_relief():
    w, s, e, n = parse_kml(KML_FILENAME)
    dem, affine = download_dem(w, s, e, n)
    
    print("🪄 Згладжування поверхні...")
    dem = gaussian_filter(dem, sigma=SURFACE_SMOOTHING)
    
    # Проектор (WGS84 -> EPSG:5569)
    project = Transformer.from_crs("EPSG:4326", TARGET_EPSG, always_xy=True).transform
    
    min_h = np.floor(np.nanmin(dem) / 10) * 10
    max_h = np.nanmax(dem)
    levels = np.arange(min_h, max_h + CONTOUR_INTERVAL, CONTOUR_INTERVAL)

    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    
    processed_levels = []
    
    for level in levels:
        fig, ax = plt.subplots()
        cs = ax.contourf(dem, levels=[level, max_h + 5000])
        paths = cs.get_paths()
        plt.close(fig)

        level_polygons = []
        for path in paths:
            for p_coords in path.to_polygons():
                if len(p_coords) < 3: continue
                metric_pts = [project(*(affine * (c[0], c[1]))) for c in p_coords]
                poly = Polygon(metric_pts)
                if poly.is_valid:
                    level_polygons.append(poly)
        
        if level_polygons:
            combined = unary_union(level_polygons)
            parts = [combined] if isinstance(combined, Polygon) else list(combined.geoms)
            processed_levels.append((level, parts))

    # Межі для прев'ю
    all_geoms = [p for lvl, parts in processed_levels for p in parts]
    map_bounds = unary_union(all_geoms).bounds
    m_min_x, m_min_y = map_bounds[0], map_bounds[1]
    map_h_mm = (map_bounds[3] - map_bounds[1]) * USER_SCALE

    curr_x, curr_y = 0, -(map_h_mm + 100) 
    row_max_h = 0

    print(f"📐 Побудова DXF з висотою підпису {LABEL_HEIGHT}мм...")

    for level, parts in processed_levels:
        layer_name = f"LEVEL_{int(level)}"
        if layer_name not in doc.layers:
            doc.layers.new(name=layer_name)

        for geom in parts:
            def add_to_dxf(g, off_x, off_y):
                def scale_pt(pt):
                    return ((pt[0] - m_min_x) * USER_SCALE + off_x, 
                            (pt[1] - m_min_y) * USER_SCALE + off_y)

                # Контури
                ext = [scale_pt(p) for p in g.exterior.coords]
                msp.add_lwpolyline(ext, close=True, dxfattribs={'layer': layer_name})
                for interior in g.interiors:
                    int_pts = [scale_pt(p) for p in interior.coords]
                    msp.add_lwpolyline(int_pts, close=True, dxfattribs={'layer': layer_name})
                
                # Текст підпису (використовуємо LABEL_HEIGHT)
                lbl = g.representative_point()
                lx, ly = scale_pt((lbl.x, lbl.y))
                msp.add_text(
                    f"{int(level)}", 
                    dxfattribs={
                        'height': LABEL_HEIGHT, 
                        'layer': layer_name
                    }
                ).set_placement((lx, ly))

            # 1. Загальний вид
            add_to_dxf(geom, 0, 0)

            # 2. Розкладка деталей
            minx, miny, maxx, maxy = geom.bounds
            w_mm = (maxx - minx) * USER_SCALE
            h_mm = (maxy - miny) * USER_SCALE

            if curr_x + w_mm > MAX_ROW_WIDTH:
                curr_x = 0
                curr_y -= (row_max_h + MARGIN)
                row_max_h = 0

            det_off_x = curr_x - (minx - m_min_x) * USER_SCALE
            det_off_y = curr_y - (miny - m_min_y) * USER_SCALE
            add_to_dxf(geom, det_off_x, det_off_y)

            curr_x += w_mm + MARGIN
            row_max_h = max(row_max_h, h_mm)

        curr_x = 0
        curr_y -= (row_max_h + MARGIN * 2)
        row_max_h = 0

    doc.saveas("dem_to_contours.dxf")
    print(f"✅ Готово! Файл 'DEM_to_contours.dxf' створено.")

if __name__ == "__main__":
    try:
        process_relief()
    except Exception as e:
        print(f"❌ Помилка: {e}")